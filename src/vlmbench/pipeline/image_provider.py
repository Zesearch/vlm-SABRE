"""Provider-neutral image generation and editing contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ImageGenerationClient(Protocol):
    provider: str
    model: str

    def generate(self, *, prompt: str) -> GeneratedImage:
        """Generate one image from text."""

    def edit(self, *, source_image: Path, prompt: str) -> GeneratedImage:
        """Edit one source image according to a controlled prompt."""

