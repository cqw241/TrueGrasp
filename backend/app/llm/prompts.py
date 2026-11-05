"""Reusable templates; each Agent supplies its own isolated context."""

from string import Template

from app.contracts.llm import ChatMessage


class PromptTemplates:
    def __init__(self, templates: dict[str, str]) -> None:
        self.templates = {name: Template(text) for name, text in templates.items()}

    def render(self, name: str, **values: str) -> ChatMessage:
        return ChatMessage(role="system", content=self.templates[name].substitute(values))
