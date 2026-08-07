"""Built-in task-template discovery and lightweight routing."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class TaskTemplate:
    template_id: str
    title: str
    description: str
    keywords: tuple[str, ...]
    content: str


TEMPLATE_METADATA = {
    "context_prior": {
        "title": "Context-prior paired edit",
        "description": (
            "Replace one context-expected source object with a visually plausible but "
            "context-unexpected target and construct four presence probes."
        ),
        "keywords": (
            "context prior",
            "context-prior",
            "unexpected object",
            "replace object",
            "source object",
            "target object",
        ),
    },
}


class TemplateRegistry:
    """Load packaged templates and select only those relevant to one task."""

    def __init__(self, templates: list[TaskTemplate] | None = None) -> None:
        self._templates = {
            template.template_id: template
            for template in (templates if templates is not None else self._load_packaged())
        }

    @staticmethod
    def _load_packaged() -> list[TaskTemplate]:
        root = files("vlmbench.agent").joinpath("templates")
        templates: list[TaskTemplate] = []
        for template_id, metadata in TEMPLATE_METADATA.items():
            content = root.joinpath(f"{template_id}.md").read_text(encoding="utf-8")
            templates.append(
                TaskTemplate(
                    template_id=template_id,
                    title=str(metadata["title"]),
                    description=str(metadata["description"]),
                    keywords=tuple(metadata["keywords"]),
                    content=content,
                )
            )
        return templates

    def get(self, template_id: str) -> TaskTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._templates))
            raise ValueError(
                f"Unknown template {template_id!r}. Available templates: {available}"
            ) from exc

    def select(
        self,
        markdown: str,
        requested: list[str] | None = None,
        limit: int = 2,
    ) -> list[TaskTemplate]:
        if requested:
            return [self.get(template_id) for template_id in dict.fromkeys(requested)]
        lowered = markdown.lower()
        scored = [
            (
                sum(keyword in lowered for keyword in template.keywords),
                template.template_id,
                template,
            )
            for template in self._templates.values()
        ]
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in scored if row[0] > 0][:limit]

    def summaries(self) -> list[dict[str, str]]:
        return [
            {
                "template_id": template.template_id,
                "title": template.title,
                "description": template.description,
            }
            for template in sorted(
                self._templates.values(), key=lambda item: item.template_id
            )
        ]
