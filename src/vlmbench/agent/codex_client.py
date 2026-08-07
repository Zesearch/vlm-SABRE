"""Thin adapter around the open-source Codex Python SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


class CodexTaskDesignClient:
    """Run schema-constrained task-design turns in a persistent Codex thread."""

    provider = "codex"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "high",
        working_directory: Path | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("OpenAI API key is required for the Codex task-design agent.")
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise ValueError("OpenAI base URL is required for the Codex task-design agent.")
        self.model = model.strip()
        if not self.model:
            raise ValueError("OpenAI agent model is required for the Codex task-design agent.")
        self.reasoning_effort = reasoning_effort.strip()
        self.working_directory = (working_directory or Path.cwd()).resolve()
        self._codex = None
        self._thread = None

    def __enter__(self) -> CodexTaskDesignClient:
        self._ensure_thread()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        try:
            from openai_codex import Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "Codex task-design support is not installed. Run "
                "`python -m pip install -e '.[agent]'`."
            ) from exc

        self._codex = Codex(
            CodexConfig(
                config_overrides=(
                    f"openai_base_url={json.dumps(self.base_url)}",
                )
            )
        )
        self._codex.__enter__()
        kwargs = {
            "config": {"model_reasoning_effort": self.reasoning_effort},
            "cwd": str(self.working_directory),
            "ephemeral": True,
            "sandbox": Sandbox.read_only,
        }
        kwargs["model"] = self.model
        try:
            # Always select API-key authentication explicitly. This prevents the
            # SDK from silently reusing a local Codex/ChatGPT account session.
            self._codex.login_api_key(self.api_key)
            self._thread = self._codex.thread_start(**kwargs)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._codex is not None:
            self._codex.__exit__(None, None, None)
        self._codex = None
        self._thread = None

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[ModelT],
    ) -> ModelT:
        self._ensure_thread()
        prompt = (
            "Task-design compiler instructions:\n\n"
            f"{system_prompt.strip()}\n\n"
            "Current user task:\n\n"
            f"{user_prompt.strip()}"
        )
        turn = self._thread.turn(
            prompt,
            output_schema=schema.model_json_schema(),
        )
        result = turn.run()
        raw = str(result.final_response or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Codex returned invalid structured output: {raw[:500]!r}"
            ) from exc
        return schema.model_validate(payload)
