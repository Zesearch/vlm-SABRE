"""Gemini image-generation and editing provider."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from vlmbench.pipeline.gemini_client import image_mime
from vlmbench.pipeline.image_provider import GeneratedImage


THREAD_LOCAL = threading.local()


def response_image(response: Any) -> GeneratedImage:
    for candidate in response.candidates or []:
        for part in ((candidate.content.parts if candidate.content else []) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data and str(inline.mime_type or "").startswith("image/"):
                return GeneratedImage(
                    data=bytes(inline.data),
                    mime_type=str(inline.mime_type),
                )
    raise RuntimeError("Gemini returned no image.")


class GeminiImageClient:
    provider = "google"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        aspect_ratio: str = "3:2",
        resolution: str = "2K",
        require_native_resolution: bool = True,
        allow_1k: bool = False,
        retries: int = 3,
        timeout_ms: int = 180_000,
    ) -> None:
        if not model.strip():
            raise ValueError("Gemini image model is required.")
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self.api_key = api_key
        self.model = model.strip()
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        self.require_native_resolution = require_native_resolution
        self.allow_1k = allow_1k
        self.retries = retries
        self.timeout_ms = timeout_ms
        self.image_config = self._image_config()

    def _image_config(self) -> types.ImageConfig:
        fields = types.ImageConfig.model_fields
        kwargs: dict[str, Any] = {"aspect_ratio": self.aspect_ratio}
        if "image_size" in fields:
            kwargs["image_size"] = self.resolution
        elif (
            self.resolution != "1K"
            and self.require_native_resolution
            and not self.allow_1k
        ):
            raise RuntimeError(
                "Installed google-genai SDK does not expose ImageConfig.image_size; "
                f"native {self.resolution} generation cannot be guaranteed. "
                "Upgrade google-genai or pass --allow-1k."
            )
        return types.ImageConfig(**kwargs)

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

    def _request(self, contents: list[Any]) -> GeneratedImage:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._client().models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=self.image_config,
                    ),
                )
                image = response_image(response)
                return GeneratedImage(
                    data=image.data,
                    mime_type=image.mime_type,
                    metadata={
                        "aspect_ratio": self.aspect_ratio,
                        "requested_resolution": self.resolution,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 * attempt, 8))
        raise RuntimeError(
            f"Gemini image request failed after {self.retries} attempt(s): {last_error}"
        ) from last_error

    def generate(self, *, prompt: str) -> GeneratedImage:
        if not prompt.strip():
            raise ValueError("Image-generation prompt is empty.")
        return self._request([prompt.strip()])

    def edit(self, *, source_image: Path, prompt: str) -> GeneratedImage:
        if not prompt.strip():
            raise ValueError("Image-edit prompt is empty.")
        image_part = types.Part.from_bytes(
            data=source_image.read_bytes(),
            mime_type=image_mime(source_image),
        )
        return self._request([prompt.strip(), image_part])

