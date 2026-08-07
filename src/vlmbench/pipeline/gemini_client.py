"""Gemini provider adapter for pressure screening."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from vlmbench.eval import infer_eval_type


THREAD_LOCAL = threading.local()


def clean_api_key(raw: str) -> str:
    key = raw.strip()
    if "=" in key:
        key = key.split("=", 1)[1].strip()
    if (key.startswith('"') and key.endswith('"')) or (
        key.startswith("'") and key.endswith("'")
    ):
        key = key[1:-1].strip()
    return key


def resolve_gemini_key(dataset_root: Path, explicit: Path | None = None) -> str:
    candidates = [explicit] if explicit else []
    candidates.extend(
        parent / "gemini_api_key.txt" for parent in [dataset_root, *dataset_root.parents]
    )
    for path in candidates:
        if path and path.exists():
            key = clean_api_key(path.read_text(encoding="utf-8"))
            if key:
                return key
    key = clean_api_key(os.environ.get("GEMINI_API_KEY", ""))
    if key:
        return key
    raise EnvironmentError("Set GEMINI_API_KEY or pass --api-key-file.")


def image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    raise ValueError(f"Unsupported image type: {path}")


def screening_prompt(question: dict[str, Any]) -> str:
    prompt = str(question.get("prompt") or question.get("question") or "").strip()
    if not prompt:
        raise ValueError(f"Question {question.get('question_id', '')} has no prompt.")
    options = question.get("options") or {}
    if options:
        option_lines = "\n".join(f"{key}. {value}" for key, value in options.items())
        prompt = f"{prompt}\n\nOptions:\n{option_lines}"

    eval_type = infer_eval_type(question)
    instruction = {
        "yes_no_exact": "Answer with only yes or no.",
        "count_exact": "Answer with only the count.",
        "choice_exact": "Answer with only the option letter.",
        "multiple_choice_exact": "Answer with only the option letter.",
        "contains": "Answer with the shortest possible phrase.",
        "exact": "Answer with the shortest possible phrase.",
        "exact_match": "Answer with the shortest possible phrase.",
        "short_answer_exact": "Answer with the shortest possible phrase.",
    }.get(eval_type)
    if not instruction:
        raise ValueError(f"Unsupported eval_type for Gemini screening: {eval_type}")
    return f"Question: {prompt}\n\n{instruction} Do not explain."


class GeminiPredictionClient:
    provider = "google"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_output_tokens: int = 64,
        timeout_ms: int = 120_000,
        retries: int = 3,
    ) -> None:
        if not model.strip():
            raise ValueError("Gemini model is required.")
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self.api_key = api_key
        self.model = model.strip()
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.timeout_ms = timeout_ms
        self.retries = retries

    def _client(self) -> genai.Client:
        client = getattr(THREAD_LOCAL, "client", None)
        client_key = getattr(THREAD_LOCAL, "api_key", None)
        if client is None or client_key != self.api_key:
            client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=self.timeout_ms),
            )
            THREAD_LOCAL.client = client
            THREAD_LOCAL.api_key = self.api_key
        return client

    def predict(self, *, image_path: Path, question: dict[str, Any]) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._client().models.generate_content(
                    model=self.model,
                    contents=[
                        types.Part.from_bytes(
                            data=image_path.read_bytes(),
                            mime_type=image_mime(image_path),
                        ),
                        screening_prompt(question),
                    ],
                    config=types.GenerateContentConfig(
                        temperature=self.temperature,
                        candidate_count=1,
                        max_output_tokens=self.max_output_tokens,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                raw = str(response.text or "").strip()
                if not raw:
                    raise RuntimeError("Gemini returned an empty response.")
                return raw
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
        raise RuntimeError(
            f"Gemini screening failed after {self.retries} attempt(s): {last_error}"
        ) from last_error

